# How an agent's use of a graph database changes as the graph grows

An LDBC-FinBench-shaped anti-money-laundering graph, generated at three scales, with six
money-laundering typologies planted in it at known positions. Thirteen questions, seven agent
designs, 819 measured episodes, and then every query the four scale-axis designs settled on
replayed a hundred times without a model in the loop so the latency figures mean something.
Below the episodes, the interface itself is measured — transport, decoder, runtime, and the
encoding rows wear on their way into the model — so that "why does it get better" has
numbers at every layer.

The thing being measured is not the model and not the database. It is the **exchange** between
them: one question in, an unknown number of Cypher round trips out, and what that shape does
when the graph gets a hundred times bigger.

---

## The headline

*(this six-panel chart is retired from the repo — regenerate with
`python scripts/plot_interaction.py`. The committed deck is
`figures/overview-p50.svg` / `overview-p99.svg` — the same contract chain, difficulty
pooled, p50 from live calls and p99 from the stage-two replays — narrated in
`docs/slides.md`.)*

Six panels — who is asking, by how hard the question is. Scale factor across, p99 latency up,
one line per agent design, questions within a cell averaged geometrically. **A filled marker
means every episode in the cell matched the gold answer; hollow means none did; grey means
some.** Correctness is on the chart because a latency plot on its own rewards the design that
is cheap for the wrong reason — and one cell here is exactly that case.

What the six panels say:

- **easy, either audience** — the four designs sit on top of each other and all are filled.
  For a one-hop question, what the agent is told does not matter.
- **external / medium** — the separation appears, and it is not only cost. The red line
  (labels only) is *hollow at all three scales* and also the highest. Both wrong and slowest,
  and the gap widens as the graph grows.
- **external / hard** — the green line (plan feedback) sits an order of magnitude below the
  other three at every scale, and stays filled.
- **internal / hard** — grey markers everywhere. Nothing solves the three-layer conjunction
  reliably at any scale, and the reason is not what you would guess. See finding 2.

Per-question detail, for checking any single claim above, regenerates alongside it.

---

## What was measured

**Four axes**, chosen so that each one isolates something an operator can actually change.

### 🏗️ Workflow & SEOCHO-Driven Middleware Architecture

SEOCHO operates as a specialized **Database-Agent Operating System (Middleware)** managing the bidirectional contract between the Language Model and the Graph Database Engine. It intervenes across three distinct planes (Generation, Execution, and Return) to ensure semantic precision, multi-tenant safety, and strict p99 SLO guarantees at scale:

```mermaid
flowchart TD
    UserReq["User Question: High-Risk Channel Transfers"]
    
    subgraph SEOCHO_OS["SEOCHO Middleware and OS Runtime"]
        direction TB
        
        subgraph PLANE_GEN["Plane 1: Generation Plane"]
            OntoLoader["SEOCHO Ontology Loader"]
            PromptSynth["Contextual Schema Synthesis: Entity and Edge Props, Degree Tail, Tenant Scope"]
            OntoLoader --> PromptSynth
        end
        
        subgraph PLANE_EXEC["Plane 2: Execution and Safety Plane"]
            GuardAST["SEOCHO Guardrail and AST Inspector: Whitelist, Tenant Scope, Read-Only"]
            GOpt["SEOCHO GOpt AST Optimizer: TypeFilterRemovalRule and Hub IndexHook"]
            PlanGate["SEOCHO Plan Gate and Cost Prober: EXPLAIN Parser, Cartesian Check, 2s Probe"]
            GuardAST --> GOpt
            GOpt --> PlanGate
        end
        
        subgraph PLANE_RET["Plane 3: Return and Context Plane"]
            CSVEncoder["SEOCHO Return Plane Serializer: Zero-Overhead CSV and Boundedness Signal"]
        end
    end
    
    LLM_GEN["LLM Text2Cypher Generation: gpt-oss-120b"]
    LLM_SYNTH["Augmented RAG Synthesis: Final Answer Generation"]
    
    DRIVER["High-Performance Transport: neo4j-rust-ext / neo4rs Zero-Copy Bolt v5"]
    GDBMS[("GDBMS Engine: Neo4j / DozerDB / DuckDB")]
    
    UserReq --> PromptSynth
    PromptSynth -->|Synthesized Schema| LLM_GEN
    LLM_GEN -->|Generated Cypher| GuardAST
    
    GuardAST -.->|AST Violation Feedback Loop| LLM_GEN
    PlanGate -.->|Plan Rejection and Hint Loop| LLM_GEN
    
    PlanGate -->|Validated and Optimized Cypher| DRIVER
    DRIVER --> GDBMS
    GDBMS -->|Raw Record Stream| DRIVER
    DRIVER --> CSVEncoder
    CSVEncoder -->|Compressed CSV Context: 65 percent Token Savings| LLM_SYNTH
    LLM_SYNTH --> FinalAns["Final User Answer Delivery"]
```

---

### 🔬 The 4 Agent Designs (Arms) & Prompt Injection Matrix

Every condition intervenes at exactly one plane of the agent↔database exchange. The table below illustrates the exact contract difference and prompt payload across the four core arms:

| Agent Design (Arm) | Prompt Payload Injected into LLM | SEOCHO OS Intervention Point | Primary Failure Mode Prevented | Scale Robustness (SF1 ➔ SF100) |
| :--- | :--- | :--- | :--- | :---: |
| **① `labels only`** | Raw node/rel names only:<br/>`Nodes: Account, Channel, Medium`<br/>`Edges: TRANSFER, OWN, GUARANTEE` | Baseline (No intervention) | **Referential Ambiguity**: Confuses edge properties (`TRANSFER.channel_risk`) with node properties (`Channel.risk_weight`). | ❌ **0~33% Accuracy** at all scales (Finding 1) |
| **② `+ ontology`** | Full semantic schema:<br/>• Exact property locations<br/>• Relationship direction roles<br/>• Measured degree tail metadata<br/>• Tenant scope contract (`_workspace_id`) | **Generation Plane**:<br/>`schema_for_prompt()` dynamically binds verified graph semantics. | **Schema Hallucination & Mutual Trap**: Resolves attribute confusion and bidirectional guarantee cycles (Finding 2). | ✅ **100% Accuracy** across SF1, SF10, SF100 |
| **③ `+ guardrail`** | Same prompt as `+ ontology` | **Pre-flight Execution Plane**:<br/>AST Visitor validates whitelist, tenant boundary, and read-only safety before DB hit. | **Unscoped Leaks & Injection**: Catches unparameterized queries and syntax deviations, feeding violation back to LLM turn. | ✅ **Zero Execution Violations** |
| **④ `+ plan feedback`** | Same prompt + EXPLAIN Plan Gate Operator Tree feedback upon cost refusal | **Execution & Safety Plane**:<br/>Runs `EXPLAIN`, checks for Cartesian explosion / AllNodesScan, enforces 2s latency budget. | **Hub Expansion Explosion**: Replaces 7.6s full-scan traversals with `USING INDEX` anchor seeks (Finding 1). | ⚡ **368ms p99 Latency** at SF100 (20.7x speedup) |
| **⑤–⑦ Return Plane** | Token-bounded CSV representation with `# count=N cap=K more_available=B` | **Return Plane**:<br/>Serializes graph rows into lean CSV with truncation semantics. | **Context Burn & Silent Truncation**: Reduces context consumption by 65% while preserving completeness awareness. | 📉 **65% Token Reduction** vs JSON |

| | external (public service) | internal (investigator) |
|---|---|---|
| scope | one account, subject-scoped | the whole graph |
| shape | anchored lookup | unanchored pattern search |
| bound | a request SLO | completeness |
| what scale breaks | latency, via the anchor's degree | precision, via false positives |

### How hard — the rule, and where the labels depart from it

The rule as written: `easy` is one hop and one relationship type; `medium` is two hops, or one
hop joined to a second layer; `hard` is three or more layers in conjunction, or an unanchored
pattern search.

Auditing the labels against the reference queries shows the rule does not fully account for
them, and the summary chart averages by these labels, so the discrepancy is stated here rather
than left for a reader to find:

| question | label | relationship types | variable-length | anchored | pattern hops |
|---|---|---|---|---|---|
| `ext_easy_1`, `ext_easy_2` | easy | TRANSFER | – | yes | 1 |
| `ext_med_1` | medium | **TRANSFER only** | – | yes | **1** |
| `ext_med_2` | medium | OWN, TRANSFER | – | yes | 2 |
| `ext_hard_1`, `ext_hard_2` | hard | **TRANSFER only** | **yes** | yes | 1 |
| `int_easy_1` | easy | – | – | no | 0 |
| `int_easy_2` | easy | USES_CHANNEL | – | **no** | 1 |
| `int_med_1` | medium | OWN, TRANSFER | – | no | 3 |
| `int_med_2` | medium | TRANSFER only | – | **no** | 1 |
| `int_hard_1`, `int_hard_1b` | hard | GUARANTEE, OWN, SIGN_IN, TRANSFER | – | no | 6 |
| `int_hard_2` | hard | OWN, TRANSFER | – | no | 2 |

Three departures, and each one names a kind of difficulty the rule does not:

- **`ext_med_1`** is one relationship type, one hop, anchored — `easy` by the rule. It is
  labelled medium because `channel_risk` plausibly lives in two places (on the transfer, and
  on the `Channel` node), so the question is **referentially ambiguous**. That ambiguity is
  what the labels-only design gets wrong.
- **`ext_hard_1` and `ext_hard_2`** are also one relationship type and anchored. They are hard
  because `*1..2` expansion from a power-law anchor is expensive — **traversal cost**, not
  structural depth.
- **"unanchored"** cannot discriminate within the internal class, because every internal
  question is unanchored by construction. That clause of the rule does no work.

So the axis mixes three notions: structural depth, traversal cost, and referential ambiguity.
The consequence for the summary chart is concrete — **`external / hard` measures traversal
cost** (both its questions are variable-length expansions) while **`internal / hard` measures
structural depth** (two of its three are four-relationship conjunctions). They are not the
same kind of hard, which is why the plan-feedback design pulls away in one and not the other.
Re-labelling would mean re-running all 468 episodes; publishing the audit costs nothing and
lets a reader check the label instead of trusting it.

### Four real episodes, end to end

`docs/walkthroughs.md` (regenerate with `python scripts/dump_walkthroughs.py`) prints
the runs underneath the charts: the complete prompt each design carries, then
`ext_med_1` solved four ways at SF10 — including the one where a list of label names
sends the model to the wrong property and it answers a different question plausibly —
and the eight-round-trip episode where the plan gate refuses, the agent overrides with
`accept-cost`, times out, and still cannot answer, because the question itself was
ambiguous.

### Gold, anchors, and what counts as correct

Everything needed to audit a cell without reading the harness. (The code stays the source
of truth: `QUESTIONS`, `score()`, `parse_answer()` and `discloses_truncation()` in
`scripts/agent_interaction.py`.)

- **The anchor** is chosen per database, deterministically: take the p99 of the annotated
  `_out_degree`, then the smallest `acct_no` at or above it. A hub by construction — the
  external questions are supposed to get harder with scale *through the anchor's degree*.
  External questions bind it as `$a`; internal questions run unanchored.
- **Gold is computed, never written.** Every question ships its reference Cypher (the
  `ref` field beside the question text). At run start it executes against the same
  database, anchor and workspace the episodes will use, and those rows are the gold.
- **Scalar questions** are correct when every named key's value appears among the numbers
  the answer carries, within 0.1% (or ±0.5 absolute) — tolerant of a model that rounds a
  sum, not of one that computed a different sum.
- **List questions** are correct only at recall 1.0 against the gold set, with F1 and
  precision reported beside the verdict. A partial list is a partial answer — and the
  questions are shaped as top-5 under a total order precisely so the gold answer does not
  grow with the graph, which would make "correct" mean something different at each scale.
- **Unparseable replies** (no `ANSWER:` line that parses as JSON or a bare number) score
  wrong. Episodes that died on the turn budget carry the error instead and are excluded
  from silent-truncation accounting — running out of turns is the harness's limit, not
  the model's silence.
- **Truncation disclosure** is a deliberately broad regex over the reply's prose, never
  over its numbers (a bare `50` is not a disclosure). `silent_truncation_failure`
  requires all four at once: a call hit the row cap, the answer was wrong, it was given
  anyway, and nothing in the prose said the view was bounded. Broad on purpose: a false
  disclosure hit understates the failure being counted, which is the safe direction.

### The graph the questions run against

Six node types, ten relationship types — LDBC FinBench's AML shape. Every node carries
`_workspace_id` (the tenant scope every query must bind), and `Account._out_degree` is
annotated at load time so the anchor choice and the ontology's `__cardinality__` line are
measurements, not guesses:

```mermaid
flowchart TB
    P["Person<br/>id · name · country"]
    Co["Company<br/>id · name · sector"]
    A["Account<br/>acct_no · risk_tier · flagged<br/>iban · acct_type"]
    L["Loan<br/>id · principal"]
    Ch["Channel<br/>code · label · risk_weight"]
    M["Medium<br/>id · type · risk_level"]

    A -->|"TRANSFER<br/>amount · ts · channel_risk"| A
    A -->|"WITHDRAW"| A
    P -->|OWN| A
    Co -->|OWN| A
    P -->|"GUARANTEE<br/>(also Company↔Company,<br/>directed, no reciprocal pairs)"| Co
    P -->|APPLY| L
    Co -->|INVEST| Co
    L -->|"DEPOSIT · amount"| A
    A -->|"REPAY · amount"| L
    A -->|"USES_CHANNEL · tx_count"| Ch
    M -->|SIGN_IN| A
```

Reading notes a visitor needs: `TRANSFER` is the load-bearing edge (the degree table
below is its out-degree); `OWN`, `APPLY`, `GUARANTEE` and `INVEST` accept either `Person`
or `Company` on the marked ends; `GUARANTEE` is directed and the generated graph contains
no reciprocal pair — which is what makes the `int_hard_1` / `int_hard_1b` ambiguity pair
an experiment rather than an accident (see the label audit above).

### How big

| | nodes | edges | accounts | max out-degree |
|---|---:|---:|---:|---:|
| SF1 | 4,083 | 25,061 | 2,470 | 767 |
| SF10 | 36,483 | 235,441 | 20,470 | 3,568 |
| SF100 | 360,483 | 2,340,822 | 200,470 | 16,787 |

Same generator, same seed, all ten edge types. The degree distribution is power-law by
construction: the p99 out-degree barely moves (33 → 38 → 37) while the maximum grows 22×. That
is the property that makes the hard questions hard, and it is the one a uniform-attachment
generator does not have.

### What the agent is given

Four designs, each a superset of the one above it, so the difference between two adjacent rows
is attributable to the one thing that changed.

| design | what it adds |
|---|---|
| `labels only` | label and relationship names. Plain text2cypher. |
| `+ ontology` | endpoint types, relationship direction as named roles, declared properties, measured degree hints |
| `+ guardrail` | the ontology is *enforced* on tool arguments before the query reaches the database; a violation returns to the model as text, so a rejection becomes a repair |
| `+ plan feedback` | the query runs under a 2-second probe first; one that does not finish comes back with its plan and an instruction to start from an index, plus an `/* accept-cost */` override |

---

## Results

`gpt-oss-120b` · 13 questions × 3 scales × 4 designs × 3 repeats = **468 episodes** ·
row cap 50 · transaction timeout 60 s.

### Cost

Median db hits per question. db hits rather than milliseconds, because it is the one cost unit
unaffected by what else is running on the machine.

| | SF1 | SF10 | SF100 |
|---|---:|---:|---:|
| **external** — labels only | 24,649 | 91,698 | 719,184 |
| **external** — + ontology | 9,782 | 46,332 | **218,674** |
| **internal** — labels only | 235,921 | 2,227,118 | 22,238,961 |
| **internal** — + ontology | 104,554 | 1,089,714 | **8,119,610** |

### Correctness

| design | correct |
|---|---:|
| labels only | 102/117 |
| + ontology | **108/117** |
| + guardrail | **108/117** |
| + plan feedback | 106/117 |

### The exchange does not get deeper

**Round trips stay at a median of 1.0 at every scale and for every design.** Characters
returned into the model's context stay between 109 and 169. A growing graph does not make
questions deeper and does not flood the context — it makes each hop wider. Everything that
grows, grows inside a single query.

---

## Three findings

### 1. The ontology paid for itself in both currencies at once

`ext_med_1` — *"which accounts sent money to my account over a high-risk channel?"*

| design | db hits (SF100) | p99 (SF100) | correct |
|---|---:|---:|---:|
| labels only | 1,158,923 | 116 ms | **1/9** |
| + ontology | 53,293 | 19 ms | **9/9** |

21.7× cheaper and right instead of wrong. The labels-only design reached the channel through
`Account-[:USES_CHANNEL]->Channel.risk_weight` — a defensible reading of the words, and a
different question with a different answer. The ontology declares that `channel_risk` sits on
the transfer itself.

The general point: accuracy and cost are usually owned by different dashboards and different
people. Here one intervention moved both, and **the wrong answer was also the expensive one.**

### 2. A schema that makes direction legible does not remove ambiguity — it exposes it

Two questions, identical graph, identical schema, identical model, identical scales. The only
difference is the wording.

| question | wording | labels only | + ontology |
|---|---|---:|---:|
| `int_hard_1` | "their owners **guarantee one another**" | 6/9 | **0/9** |
| `int_hard_1b` | "one of them **guarantees the other in either direction**" | 6/9 | **9/9** |

The ontology declares `GUARANTEE` as running `guarantor → guaranteed`. Told that, the model
takes the direction seriously and reads "one another" as *mutual* — writing `(o1)->(o2)` **and**
`(o2)->(o1)`. The graph holds 40,001 `GUARANTEE` edges and **not one reciprocal pair**, so the
answer is empty by construction. The labels-only design, having no direction to commit to,
wrote the undirected form and matched.

This is the same mechanism that produced finding 1, with the opposite sign. Both questions are
kept in the set for exactly that reason — one without the other supports the wrong conclusion.

### 3. Recall is free. Precision is what scale destroys.

Six planted typologies, four scale factors, on the power-law graph. **Recall is 1.00 in all
24 measurements.** Only precision moves:

| rule | SF1 | SF10 | SF100 | SF1000 |
|---|---:|---:|---:|---:|
| nominee structuring | 1.000 | 1.000 | 0.143 | **0.020** |
| layering cycle | 1.000 | 1.000 | 1.000 | **0.111** |
| loan integration | 0.001 | 0.000 | 0.000 | **0.000** |
| funnel account | 1.000 | 1.000 | 1.000 | 1.000 |
| equity integration | 1.000 | 1.000 | 1.000 | 1.000 |
| **common control** | 0.250 | 0.500 | **1.000** | **1.000** |

The two that collapse are the ones resting on a numeric predicate over a single layer.
Nominee structuring is *"owner-level aggregate of sub-threshold legs"* — and at SF1000 it
returns 49 candidates for one real case. The planted gold records why: ranking owners by total
amount **misses it**, because the planted owner sat 100th of 103 owners above the threshold.
Innocent owners reach hundreds of millions on a handful of large legitimate transfers; the ring
reaches twelve million on 468 small ones. Ordinary transfers have a median of ₩28,450 against a
₩10,000,000 reporting threshold, so *"below the reporting threshold"* describes about ninety
percent of normal traffic.

The one that **improves** is a conjunction across three independent layers — transfer, party
guarantee, shared login device — each of them unremarkable alone. The arithmetic is the whole
story: a predicate on one layer keeps a fraction *f* of the graph, so false positives grow as
*f·N*; a conjunction of *k* independent layers keeps ∏*fᵢ*, and when that product shrinks
faster than *N* grows, precision goes **up**.

So the axis that has to scale is not context window or hop count. It is **how many independent
layers the query can reach.**

---

## One level down: the interface itself

The four designs above vary what the agent is *told*. The second half of the work varies the
**exchange** — who does the arithmetic, what encoding the rows wear, and what executes under
the driver.

### Conditions 5–7: the agent does the arithmetic

Three more conditions ([exact prompt diffs](docs/conditions.md)) ban aggregate functions in
Cypher, so the rows land in context and the model computes the answer itself — the measurement
finding 4-as-planned called for. Condition 5 returns rows as JSON and *tells* the agent when a
page was cut (`more_available`); condition 6 is the control that withholds exactly that;
condition 7 is condition 5 with the rows encoded as CSV instead.

*(chart regenerates with `python scripts/plot_in_context.py`; its numbers are below)*

- **One tool-response field moves silent failures 71 → 11.** Denied `more_available`, the
  blind control answered wrongly off a truncated view *without saying so* in 71 of 117
  episodes; the told arms did that 11 and 16 times. Accuracy barely moves (39 vs 40 vs 35
  correct) — the field does not make the model right, it makes the model honest, and the
  honesty is the schema's doing, not the model's instinct.
- **CSV carries the same episodes at a third of the context** (median 3.7k vs 11.0k input
  tokens per episode at SF100) with accuracy statistically indistinguishable and *more*
  disclosure, not less. The per-row keys JSON repeats are overhead, not signal — measured
  directly: the same 200 rows are 9,017 tokens as JSON and 5,211 as CSV
  (the seven-encoding sweep regenerates with `python scripts/plot_depth.py`).
- **Telling the agent has a price**: the told arms page (up to 14 round trips) and ran out of
  their 16-turn budget 47 times; the blind arm stops early — median 5.8k input tokens per
  episode at SF100 against 11.0k for the told JSON arm — cheap and silently wrong.

Question by question (same convention as the main figures — scale across, db hits up,
marker fill carrying correctness; `python scripts/plot_in_context.py` regenerates it), the
blind arm's flat, hollow lines on the anchored questions are the failure mode drawn: one
page fetched, cheapest cost on the panel, wrong at every scale. And `int_hard_2` is what the
aggregate ban costs at the top end — 10⁸ db hits per episode where condition 4 answered the
same question through one server-side aggregate.

These arms also surfaced a serving artifact worth knowing about: on long multi-page episodes
`gpt-oss-120b` deterministically spends its closing turn in the reasoning channel and returns
empty content. The harness repairs it with one recorded nudge (`nudged` per episode; 6 of 351
fired) so the arms are measured, not the artifact.

**The disclosure effect was then tested on a second model family** — conditions 5 and 6
re-run on DeepSeek-V3.2, same questions, caps and budgets (234 episodes,
`results/in_context_deepseek.json`). The gap does not transfer, and the reason is the
finding: DeepSeek pages whether or not it is told the view is bounded (11.6 round trips in
the *blind* arm, against gpt-oss's 2.3) and instead exhausts its 16-turn budget in 54–64%
of episodes — the failure moves from *silently wrong* to *no answer*. So `more_available`
repairs a failure mode gpt-oss has and DeepSeek does not; what is model-general is that
neither model manages boundedness well unaided — one under-fetches silently, the other
over-fetches into the budget — and the contract lever that fixes it differs by model.
(DeepSeek also never triggered the empty-final nudge, confirming that artifact as
gpt-oss-specific.)

### The three boundaries of bridge 2

What a returned row costs, measured at each layer with client CPU, the DB container's cgroup
CPU, tracemalloc, and involuntary context switches (raw samples + a machine manifest per run
in `results/bench/`):

- **Transport** — a query without LIMIT costs 12 ms on
  Bolt (the client stops pulling), 398 ms on HTTP (the 2.4 MB body is already complete), and
  1.4 ms with LIMIT in the query on either. The 276× spread is closed by the contract, not the
  transport.
- **Runtime** — consuming a row costs ~7× more CPU
  than producing it (client 20.8 µs vs server 2.9 µs at 100k rows), and the cost is the
  *representation*: a row materialized as a Python dict is 346 bytes against ~30 of data, in
  both decoder builds. The rust codec is a −26% prefactor; the curve does not change.
- **Concurrency** — the same 8-worker load runs at
  p50 **769 ms** on Python threads (1.3 cores used, the GIL), **81 ms** on eight Python
  processes (7.2 cores — the control that convicts the runtime), and **7.7 ms in one native
  process** (`bench/neo4rs-bench`, tokio) at 2.5 ms CPU per call, because the rows never
  become Python objects. Control plane in Python, data plane native, is the architecture this
  measures its way toward.

### Eight levers, two kinds of fix

Everything above on one closing chart, each lever against the metric it moved, grouped by
what kind of fix it is (`python scripts/plot_levers.py` regenerates it). Compare within a
block, not across. What the grouping says: **contract-side fixes changed
what the agent does; runtime-side fixes changed what the exchange costs — and neither
substitutes for the other.** No driver makes a model disclose truncation; no prompt makes a
row cost less than 346 bytes in a Python dict.

---

## Reproducing it

**Everything semantic in this experiment is [SEOCHO](https://github.com/tteon/seocho)** — the
ontology object, the prompt schema, and the guardrail are `seocho.ontology` and
`seocho.query`, installed straight from the seocho repository by the first command below. To
rerun anything here, you set up seocho; that is the intended door. Requires Docker, Python
3.10+, and an OpenAI-compatible endpoint.

### 🚀 Unified Quickstart (AIPerf-Style Declarative Runner)

```bash
pip install -r requirements.txt

# 1. Run the unified benchmark suite via declarative YAML:
python runner.py run --config configs/aml_suite.yaml

# 2. Run a fast smoke test across all suites:
python runner.py run --config configs/quick_smoke.yaml

# 3. Launch the interactive live E2E pipeline trace demo:
python runner.py demo -p ext_med_1 -a 1001
```

### 🖥️ Running the arms on a self-hosted vLLM (rented GPU)

The published numbers come from a hosted API that has no prefix cache — verified, not
assumed: `cached_tokens` is absent and TTFT is flat across byte-identical prefixes. The one
question that needs a server of our own, "does the shared ontology prefix stop being paid
for", is measured by the testbed in [`docs/testbed.md`](docs/testbed.md): one image holding
DozerDB, vLLM and this harness, datasets and results checkpointed to S3, and an
`episodes.jsonl` the run resumes from when the instance disappears.

```bash
MODEL_PROVIDER=vllm VLLM_MODEL=openai/gpt-oss-120b VLLM_BASE_URL=http://127.0.0.1:8000/v1 \
  python runner.py run --config configs/quick_smoke.yaml   # any suite, any endpoint
NEO4J_PASSWORD=... S3_BUCKET=... testbed/bootstrap.sh      # the whole loop on a rented box
```

### 🔬 Step-by-Step Reproduction Pipeline

```bash
# 1. Generate and load three scales (all ten edge types, power-law degree)
for SF in 1 10 100; do
  python scripts/data/gen_duckdb.py --sf $SF --tag layers \
      --hub-skew 3.0 --dup-share 0.12 --closure-share 0.1 --cycle-share 0.05 \
      --out outputs/finbench
  python scripts/data/bulk_load.py --src outputs/finbench/sf${SF}-layers \
      --database finbenchl${SF} --password "$NEO4J_PASSWORD"
done

# 2. Run the agents (819 episodes across the seven conditions)
python scripts/agents/agent_interaction.py --password "$NEO4J_PASSWORD" \
    --databases finbenchl1:1 finbenchl10:10 finbenchl100:100 \
    --arms labels ontology guardrail plan in_context in_context_blind in_context_csv \
    --repeats 3 --ontology ontology/finbench.ontology.yaml \
    --out results/agent_interaction.json

# 3. Replay each settled query for a p99 (no model in the loop)
python scripts/agents/replay_p99.py --password "$NEO4J_PASSWORD" \
    --episodes results/agent_interaction.json --iterations 100 --cell-budget 45 \
    --out results/replay_p99.json

# 4. Charts and tables
python scripts/plotting/plot_interaction.py --figures figures
python scripts/plotting/plot_in_context.py --episodes results/agent_interaction.json
python scripts/analysis/report_interaction.py > docs/finbench-agent-interaction.md

# 5. The interface benchmarks (idle DB — they contend with the episodes otherwise)
python scripts/benchmarks/bench_bridge2.py --password "$NEO4J_PASSWORD" --database finbenchl1
python scripts/benchmarks/bench_driver_memory.py --password "$NEO4J_PASSWORD" --database finbenchl1
( cd bench/neo4rs-bench && NEO4J_PASSWORD=$NEO4J_PASSWORD cargo run --release -- finbenchl1 )
python scripts/plotting/plot_depth.py && python scripts/plotting/plot_levers.py
```

Every benchmark writes machine-readable results to `results/bench/` with per-iteration
samples and a manifest (git commit, decoder, driver versions, container image, CPU) via
`scripts/analysis/runmeta.py`, so any number in the figures can be traced to the machine state that
produced it. Run `bench_driver_memory.py` once in a plain-`neo4j` environment and once with
`neo4j-rust-ext` installed to reproduce the decoder comparison.

The generator is deterministic — a fixed seed gives byte-identical row counts and planted
patterns — so step 1 reproduces the same graph. Steps 2 onward involve a language model and
will not reproduce exactly; the committed `results/` are the run the numbers above come from.

The database is [DozerDB](https://dozerdb.org/) `graphstack/dozerdb:5.26.3.0` (Neo4j 5 with
the enterprise features unlocked). Model access goes through any OpenAI-compatible endpoint —
set `MARA_API_KEY` and optionally `MARA_BASE_URL`, or point `agent_interaction.py` at another
provider.

### Layout

```
runner.py                         Unified AIPerf-style declarative benchmark CLI runner
configs/                          YAML benchmark configurations (aml_suite.yaml, quick_smoke.yaml)
harness/                          Core benchmark harness and reporting engine
ontology/                         finbench.ontology.yaml and fibo_finbench.ontology.yaml
scripts/                          Categorized subdirectories:
  scripts/data/                   DuckDB generator, bulk loaders, degree annotator
  scripts/benchmarks/             SQL vs Cypher, SEOCHO GOpt, FIBO SF100, Scale Factor
  scripts/agents/                 Agent interaction harness, OpenAI Agents SDK adapter, live E2E demo
  scripts/analysis/               Interaction reporting, rescorers, token measurements
  scripts/plotting/               Talk and paper SVG chart generators
  scripts/smoke/                  SEOCHO environment sanity check
bench/neo4rs-bench/               The native end of the driver spectrum (Rust, tokio)
results/                          819 episodes, 156 replayed cells, bench JSONs + manifests
figures/                          The talk charts (overview, engineering detail, trade-offs)
docs/                             Conditions, slides, GOpt comparisons, walkthroughs, defect log
```

`docs/finbench-ontology-defects.md` is worth reading on its own: it records four defects the
experiment found by running agents against the graph and reading what they wrote, with what
each one cost. None of them was visible by inspecting the schema.

---

## What this does not show

Stated plainly, because a result without its limits is a claim.

- **Mostly one model.** The scale-axis results are `gpt-oss-120b` at temperature 0 only.
  The disclosure pair was additionally run on DeepSeek-V3.2, where its effect did not
  transfer (see conditions 5–7) — evidence that arm effects interact with the model family,
  and a reason not to generalize the other arms' numbers beyond gpt-oss without the same
  check.
- **Three repeats per cell.** The failure-mode gap is not noise — bootstrapped over
  episodes, the blind arm's silent-failure excess is +51 pp with a 95% CI of [+41, +62].
  The CSV-vs-JSON accuracy difference is −3.4 pp with a 95% CI of [−15, +9]: no detectable
  difference, but intervals this wide cannot rule out a real one either way — which is the
  honest reading of 117 episodes per arm.
- **Synthetic data.** The graph is generated to LDBC FinBench's shape and its typologies come
  from FATF and FinCEN guidance, but it is not a real institution's data. Validation against
  the LDBC FinBench reference dataset is open.
- **The interface benchmarks share one box with the database.** Client and server CPU are
  charged separately (process rusage vs container cgroup), but a cross-machine run would
  add the network back into bridge 2; the runaway asymmetry only widens there.
- **`int_hard_1` at SF100 has no in-run gold.** The hand-written reference query takes **777
  seconds** at that scale, past the run's gold timeout; it was computed separately and the
  affected episodes re-scored. That number is itself a result — the optimal query for the
  hardest question no longer returns in reasonable time, which is what an agent working inside
  a 60-second budget is up against.

---

## Provenance

The ontology, guardrail and OpenAI Agents SDK adapter live upstream in
[tteon/seocho](https://github.com/tteon/seocho), pinned by the tag
[`finbench-agent-scale-v1`](https://github.com/tteon/seocho/releases/tag/finbench-agent-scale-v1)
in `requirements.txt` — a tag rather than a branch, because the branch goes away when its pull
request merges and a squash merge lands a different commit on main. This repository holds the
experiment. Three functions do the work that findings 1 and 2 are about —
`policy_from_ontology`, `schema_for_prompt` and `validate_text2cypher_fallback` — and they are
169 lines between them, which is worth knowing before assuming the result requires a framework.

The pin is for reproducing the published results. To check that an installed seocho — the
tag, or any newer one — still runs this experiment, `python scripts/smoke_seocho.py`
exercises exactly the three functions above against this repo's ontology: the schema still
renders the facts findings 1 and 2 rest on, the guardrail still accepts a conforming query
and still refuses an unscoped one. The CSV row encoding condition 7 measures here was
upstreamed as seocho's `row_format` option
([tteon/seocho#466](https://github.com/tteon/seocho/pull/466)) — the experiment fed the
middleware, which is the direction the pin is meant to point.
