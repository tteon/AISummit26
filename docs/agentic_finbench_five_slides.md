# Five-slide quantitative story: FinBench must measure the agent interface

This is a claim outline, not a chart dataset. Every number points to raw samples under this
repository's `results/`; do not copy values into plotting code as constants.

## 1. FinBench stops at the boundary where agentic RAG begins

**Claim:** FinBench supplies a realistic graph workload, but it does not yet score the
context and evidence contract crossing Agent API ↔ Bolt.

Show the two-ledger measurement unit for one episode: model calls/tokens/handoff bytes on
one side; graph trips/PROFILE DB hits/Bolt latency/result bytes on the other. Use one trace ID
to join them. The talk's existing pilot + confirmation + framework check already provide 212
fully traced episodes; the cross-model extension adds 120 comparative episodes.

**Do not claim:** that a model leaderboard or a database throughput number alone represents
agent readiness.

## 2. More agents are not a treatment unless context or authority changes

**Claim:** Object/process topology by itself did not change the visible computation.

Across five MARA-accessible models and 40 paired cells, `staged_single` and `multi_full`
agreed on correctness in 40/40 cases. Their initial Cypher agreed in 36/40. The right visual
is a paired agreement chart, with the four nondeterministic Cypher pairs shown rather than
hidden.

**Engineering consequence:** benchmark instructions, state, history, tool authority and
concurrency. Do not score the number of Agent objects.

## 3. Context isolation changes cost and quality—and the sign depends on the model

**Claim:** A typed handoff is a real, model-dependent interface intervention.

| Model | Typed prompt-token change | Correct-count change |
| --- | ---: | ---: |
| DeepSeek-V3.1 | +23.73% | +2 |
| DeepSeek-V3.2 | -2.75% | 0 |
| gemma-4-31B-it | -11.86% | 0 |
| gpt-oss-120b | -21.14% | -2 |
| MiniMax-M2.7* | -1.95% | 0 |

`*` MiniMax is an internally paired capability-adjusted arm, not common-protocol token or
latency data. Four of five models reduced prompt tokens; four of five avoided correctness
loss. Those are not the same four in a way that licenses “compression is always safe.”

## 4. Capability limits are part of the interface contract

**Claim:** One shared token budget is not automatically a fair cross-model protocol.

Four of five models completed the common 500-token planner / 1,000-token executor protocol.
MiniMax-M2.7 produced one fully recorded structured-output failure after spending 1,258
prompt and 4,078 completion tokens; the common run was stopped instead of buying 23 more
copies of the same incompatibility. At 1,200/4,000 stage budgets it completed 24 paired
episodes as a named capability-adjusted arm.

**Engineering consequence:** Agentic FinBench needs a capability handshake and must report
protocol conformance separately from task quality. Adjusted runs are new arms, never silent
replacements.

## 5. Agentic FinBench should score a versioned contract with a dual ledger

**Claim:** Approving an agent answer requires both semantic correctness and bounded systems
cost.

Score four durable objects: `QueryIntent`, harness-owned `ExecutionRequest`, typed
`ResultEnvelope`, and policy-owned `VerificationDecision`; propagate `TraceContext` through
model and Bolt calls. For every query retain raw full PROFILE tree, DB hits, Bolt phase
timings and result bytes. For every run retain endpoint/model descriptor and host + database
container samples. Add self-hosted vLLM only as a separate arm when GPU, KV-cache and
scheduler telemetry is required.

**Closing sentence:** “FinBench gives us the graph; Agentic FinBench must benchmark the
contract that decides what the model sees, what the database is allowed to execute, and how
the evidence comes back.”

## Sources in this repository

- cross-model raw data: `results/episodes/agent_model_matrix/20260831T_mara_model_matrix_v1/`
- cross-model paired analysis: `results/analysis/agent_model_matrix_20260831.json`
- confirmatory analysis: `results/analysis/agent_interface_readiness_20260831_confirmatory.json`
- experiment interpretation and telemetry scope: `docs/agent_interface_experiment.md`
