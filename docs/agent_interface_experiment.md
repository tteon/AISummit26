# FinBench in the agentic RAG era: Agent API ↔ Bolt interface experiment

## Outcome

The SF1/MARA pilot and two-repeat confirmation do **not** support “more agents” or “more
ontology” as general improvements. They support a narrower engineering claim:

> FinBench is a useful agentic workload, but Bolt rows and exceptions are not by themselves
> an agent coordination contract. Correctness and cost depend on the context and result
> envelopes placed around Bolt.

The first run is an exploratory pilot (one repeat, SF1); the second preserves two paired
repeats but is still too small for a confidence-interval claim. Every number below is derived
from raw samples in this repository. All 212 main episodes across the pilot, confirmation and
framework manipulation check have both a local JSONL trace and a Tempo-resolvable trace ID.

## Treatments

The topology run holds the MARA endpoint/model, question, physical ontology, anchor,
workspace, row cap, validator, temperature and database fixed.

| Arm | Topology | Context | Bolt result contract |
| --- | --- | --- | --- |
| `direct_single` | one generation | question only | legacy rows |
| `staged_single` | one logical agent, three stages | full transcript | legacy rows |
| `multi_full` | three role labels | full transcript | legacy rows |
| `multi_typed` | three role labels | typed isolated handoff | legacy rows |
| `multi_envelope` | three role labels | typed isolated handoff | exact `ResultEnvelope` |

`staged_single` and `multi_full` intentionally receive byte-identical visible inputs. On a
stateless Chat Completions endpoint, separate Python Agent objects are not a treatment unless
instructions, history, tools, state or concurrency differ. Their equality is a manipulation
check, not evidence that multi-agent systems never matter.

The FIBO run fixes the physical schema and changes only semantic context:

| Arm | Semantic context |
| --- | --- |
| `physical_only` | executable FinBench schema only |
| `compiled_fibo` | the complete versioned logical→physical projection |
| `retrieved_fibo` | top lexical semantic cards selected from the question and FIBO anchor |

FIBO is pinned to EDM Council `master_2026Q2`, commit
`f59157fe156e3d91b1c045222d0a7dc06b7d78a2`. A card says exact/proxy,
informative-only, local extension or unsupported. It never silently turns semantic
similarity into a physical edge.

## Confirmatory repeats

The confirmation fixed source commit `f26e85d`, endpoint, decoder settings, SF1 anchor 108,
workspace, row cap and physical graph. Arm order was randomized within paired blocks. The
topology manifest has `git_dirty=false`; its 56 raw samples and 56 conversations are present.

| Topology arm | Correct | Errors | Prompt tokens | Handoff chars | Graph trips | DB hits |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| direct single | 8/14 | 0 | 16,234 | 0 | 14 | 1,014,062 |
| multi full | 10/14 | 0 | 56,992 | 112,622 | 14 | 8,449,486 |
| multi typed | 8/14 | 2 | 45,004 | 78,030 | 12 | 3,237,206 |
| multi envelope | 8/14 | 2 | 45,842 | 80,752 | 12 | 3,237,206 |

Typed isolation reduced prompt tokens 21.03% and handoff characters 30.72% against full
context, but lost two correct paired episodes. Both errors preserved three attempted calls
and their token/trace accounting; both were deterministic guardrail refusals of the invented
physical property `Account.account_id`. Among the 12 pairs executed in both arms, median
paired DB-hit change was -2.11%. The advisory verifier falsely rejected 18 correct results
among 38 completed evaluations and falsely accepted none, reinforcing that it is a critic,
not an authority.

| FIBO context arm | Correct | Errors | Prompt tokens | Semantic chars | DB hits |
| --- | ---: | ---: | ---: | ---: | ---: |
| physical only | 12/26 | 4 | 15,584 | 0 | 122,058 |
| compiled FIBO | 12/26 | 0 | 38,548 | 99,450 | 1,387,624 |
| retrieved FIBO | 10/26 | 5 | 26,198 | 34,500 | 1,375,228 |

Compiled FIBO gained two paired episodes and lost two; retrieved FIBO gained two and lost
four. Retrieval again cut semantic context 65.31% versus the full projection and reduced its
prompt tokens 32.04%, but did not retain its aggregate correctness. `structuring_fanin` was a
repeatable gain (physical 0/2, both semantic arms 2/2), while the erroneous `ubo_chain`
mapping again cost 668,225 DB hits per semantic episode. The physical arm itself changed on
`ubo_chain` relative to the pilot, a concrete reminder that temperature zero on a hosted
endpoint does not make episodes deterministic.

The FIBO manifest's `git_dirty=true` is scoped by
`PREEXISTING_DIRTY_SCOPE.json`: the only pre-existing path was the completed topology result
directory in the same detached worktree; source/config/prompt files matched `f26e85d`.
Re-running 78 paid episodes solely to toggle that metadata bit would add no causal evidence.

## Cross-model reproduction

On 2026-08-31 the MARA discovery endpoint exposed five models. Four completed the identical
protocol (four questions × three arms × two repeats = 24 episodes per model). MiniMax-M2.7
exhausted the common 500/1,000-token decision/executor limits while reasoning and did not
return the required JSON, so its default sample is retained as a capability-boundary result,
not discarded. It then completed the same paired design as an explicitly separate
capability-adjusted arm at 1,200/4,000 tokens. Its absolute token and latency values must not
be pooled with the common protocol.

| Model | Protocol | Typed prompt change vs full | Typed handoff change | Correct-count change |
| --- | --- | ---: | ---: | ---: |
| DeepSeek-V3.1 | common | +23.73% | -19.67% | +2 |
| DeepSeek-V3.2 | common | -2.75% | -5.26% | 0 |
| gemma-4-31B-it | common | -11.86% | -14.75% | 0 |
| gpt-oss-120b | common | -21.14% | -38.71% | -2 |
| MiniMax-M2.7 | capability-adjusted | -1.95% | +9.27% | 0 |

The manipulation check is the stable result: across all 40 within-model staged-single vs
multi-full pairs, correctness agreed 100%. Initial Cypher agreed in 36/40 pairs (90%); the
four differences show that a hosted temperature-zero endpoint is still not perfectly
deterministic. Typed context reduced prompt tokens in four of five internally paired models
and avoided a correctness loss in four of five, but the model that saved the most prompt
tokens (`gpt-oss-120b`) lost two correct episodes, while DeepSeek-V3.1 gained two correct
episodes despite spending 23.73% more prompt tokens. Therefore context isolation is an
interface treatment whose effect must be measured per model, not a universal optimization.

There are 120 fully receipted comparative episodes across the five models, plus one
fully-receipted MiniMax capability gate. The interrupted default-budget MiniMax sample is
also preserved with its local trace but has no completed run receipt. All five completed
paired reports have complete local JSONL and Tempo receipts.

### Telemetry boundary and next-run contract

The completed matrix records per episode: endpoint descriptor, model-stage timings and token
usage, context/handoff sizes, generated Cypher and typed parameters, query fingerprint,
graph trips, aggregate PROFILE DB hits, Bolt availability/hydration/total timings, result
bytes, errors, and Agent→model/Agent→Bolt trace IDs. It does **not** contain the subsequently
added full PROFILE operator tree or periodic host/container samples, so those fields must not
be backfilled or inferred for this run. Hosted MARA GPU, KV-cache and scheduler telemetry is
not exposed by the API and is outside the measurable boundary.

Future topology and model-matrix runs enable two additional durable artifacts by default:

- every executed query stores the full nested PROFILE tree (operator, identifiers,
  arguments and children) beside the existing DB-hit total;
- `system_metrics.jsonl` samples client-host CPU ticks, load, memory and process RSS plus
  `docker stats` for the named database container, with wall and process-local monotonic
  clocks and an fsync-backed completion receipt.

The model-matrix runner now labels telemetry scope as local client host + database container
and explicitly marks hosted model-server telemetry unavailable. A self-hosted vLLM run is
the separate arm needed for GPU, KV residency, cache and scheduler counters. These counters
must stay in their native denominators and must not be merged with MARA observations.

## Exploratory pilot results

### Single agent, role agents and context isolation

MARA endpoint: `gpt-oss-120b`; SF1 anchor: account 108; seven diagnostic questions.

| Arm | Correct | Prompt tokens | Handoff chars | Graph trips | DB hits |
| --- | ---: | ---: | ---: | ---: | ---: |
| direct single | 5/7 | 7,227 | 0 | 7 | 1,308,917 |
| staged single | 4/7 | 30,519 | 57,862 | 7 | 531,749 |
| multi full | 4/7 | 30,519 | 57,862 | 7 | 531,749 |
| multi typed | 3/7, one refusal | 25,327 | 42,158 | 6 | 1,032,710 |
| multi envelope | 3/7, one refusal | 25,748 | 43,524 | 6 | 1,032,710 |

Interpretation:

- Staged single and multi-full produced identical correctness, prompt tokens, handoff bytes
  and initial Cypher on all seven pairs. Merely renaming stateless calls as agents changes
  nothing.
- Typed isolation reduced prompt tokens 17.01% and handoff characters 27.14% versus full
  transcript, but lost one correct answer. On `int_med_1`, the planner invented
  `account_id`; the typed handoff made that compression error authoritative and both
  executor attempts were correctly refused.
- Among the six questions executed by both full and typed arms, the median paired DB-hit
  change was 0%. One correct typed query nevertheless increased hits from 177,500 to
  812,155, so result correctness alone is not enough to approve an agent handoff.
- The exact ResultEnvelope did not change initial Cypher or correctness in this pilot. Its
  value is downstream: explicit completeness, evidence identity and pagination, which the
  current answer scorer does not reward.
- The LLM verifier falsely rejected eight correct results among 26 completed evaluations.
  It is therefore advisory. An earlier invalid gate let it trigger repairs and demonstrated
  a correct query being damaged; no production design should give this verifier write or
  re-query authority without a deterministic policy gate.

### OpenAI Agents SDK and LangGraph

The same three OpenAI Agents SDK 0.13.6 role agents were executed with a procedural scheduler
and LangGraph 0.6.11. For both full and typed context, the two schedulers produced identical
Cypher, token counts and correctness. Scheduler choice is not a quality treatment when the
state and calls are the same.

On the one-question framework manipulation check, typed context succeeded while full history
failed and used 23.26% fewer prompt tokens. This is directional evidence only; the seven-case
topology run shows that typed compression can also make an incorrect plan harder to recover.

### FIBO schema augmentation

| Arm | Correct | Errors | Prompt tokens | Semantic chars | DB hits |
| --- | ---: | ---: | ---: | ---: | ---: |
| physical only | 8/13 | 1 | 6,847 | 0 | 24,266 |
| compiled FIBO | 6/13 | 0 | 19,274 | 49,725 | 694,017 |
| retrieved FIBO | 6/13 | 1 | 14,310 | 17,250 | 693,438 |

Full FIBO context added 181.5% prompt tokens, gained no questions and lost two that the
physical schema answered. Retrieved cards reduced semantic context 65.31% and prompt tokens
25.75% relative to the full projection, but retained the same 6/13 accuracy and nearly the
same DB work.

The strongest counterexample was `ubo_chain`: physical context was correct at 2,514 DB hits;
both semantic arms invented an `Account.owner_id` join, were wrong, and consumed 668,225 hits.
This is why a valid ontology term is not automatically a valid executable mapping.

## Required interface

The results motivate a versioned contract between an Agent API and Bolt/GDBMS:

1. `QueryIntent`
   - requested entities, direction, predicates, aggregation, ordering and expected shape;
   - each semantic binding marked `exact`, `proxy`, `informative`, `unsupported` or
     `unresolved` with source/version and confidence;
   - uncertainties remain first-class instead of being compressed into authoritative names.
2. `ExecutionRequest`
   - physical Cypher template and typed named parameters;
   - workspace, anchor, row cap, timeout and budget owned by the harness/service;
   - ontology/projection version and accepted logical→physical rewrites;
   - read-only and plan-quality policy applied before Bolt execution.
3. `ResultEnvelope`
   - rows plus columns/types/units, returned count, cap, `complete|partial|unknown`, cursor;
   - evidence ID and query fingerprint;
   - scope/anchor confirmation, warnings, error taxonomy and contract version;
   - DB hits, server/client timing and result bytes as side-channel telemetry, not prompt data
     unless a later stage needs them.
4. `VerificationDecision`
   - reason codes, observed evidence and requested revision separated from authority;
   - deterministic gate decides whether another model or graph trip is permitted.
5. `TraceContext`
   - run ID, episode ID, trace ID and parent span propagated Agent→model and Agent→Bolt;
   - append-only local trace remains available if the collector is unavailable.

## Reproduction and provenance

Primary artifacts:

- confirmatory topology: `results/episodes/agent_topology/20260831T_agent_topology_confirmatory_v1/`
- confirmatory FIBO: `results/episodes/fibo_schema_context/20260831T_fibo_schema_confirmatory_v1/`
- confirmatory paired analysis: `results/analysis/agent_interface_readiness_20260831_confirmatory.json`
- topology raw/report: `results/episodes/agent_topology/20260829T_agent_topology_pilot_v2/`
- Agents SDK/LangGraph parity: `results/episodes/framework_context/20260829T_framework_parity_pilot_v1/`
- FIBO raw/report: `results/episodes/fibo_schema_context/20260829T_fibo_schema_pilot_v1/`
- paired derived analysis: `results/analysis/agent_interface_readiness_20260829.json`
- pinned projection validation: `results/analysis/fibo_projection_validation_20260829.json`
- MARA model matrix: `results/episodes/agent_model_matrix/20260831T_mara_model_matrix_v1/`
- cross-model paired analysis: `results/analysis/agent_model_matrix_20260831.json`

Invalid and interrupted gates are retained under `results/episodes/invalid_*` with an
`INVALID_REASON.md`; they are not included in any aggregate.

Relevant external references are the
[OpenAI model/agent evaluation guidance](https://developers.openai.com/api/docs/guides/latest-model),
[LangGraph subgraph/state-isolation guidance](https://docs.langchain.com/oss/python/langgraph/use-subgraphs),
and the [official EDM Council FIBO repository](https://github.com/edmcouncil/fibo).
