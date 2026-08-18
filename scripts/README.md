# 📂 Scripts Directory & Workflow Architecture

This directory contains the data generation, experiment harnesses, plotting routines, and agent adapters used in the Financial AML Graph AI benchmark.

---

## 🚀 Unified Runner (Recommended)

Instead of invoking individual benchmark scripts manually, use the **AIPerf-style declarative runner** from the project root:

```bash
# Run full benchmark suite defined in YAML:
python runner.py run --config configs/aml_suite.yaml

# Run rapid smoke test:
python runner.py run --config configs/quick_smoke.yaml

# Run interactive live E2E pipeline trace demo:
python runner.py demo -p ext_med_1 -a 1001
```

---

## 🧭 Functional Categories of Scripts

### 1. 🗄️ Data Generation & Graph ETL
| Script | Description |
| :--- | :--- |
| [`gen_duckdb.py`](file:///home/hadry/lab/AIsummit26/scripts/gen_duckdb.py) | High-speed deterministic generator creating SF1, SF10, SF100 Parquet datasets with planted AML typologies and power-law degree tails. |
| [`bulk_load.py`](file:///home/hadry/lab/AIsummit26/scripts/bulk_load.py) | Neo4j / DozerDB bulk-loader stage writer and database importer. |
| [`graph_properties.py`](file:///home/hadry/lab/AIsummit26/scripts/graph_properties.py) | Graph topology analyzer computing exact node/edge counts, clustering coefficients, and degree percentiles (p50/p90/p99/max). |
| [`annotate_ontology_degrees.py`](file:///home/hadry/lab/AIsummit26/scripts/annotate_ontology_degrees.py) | Annotates YAML ontology with empirical degree distributions extracted from generated graphs. |

---

### 2. 🧪 Benchmarks & Structural Experiments
| Script | Description |
| :--- | :--- |
| [`bench_sql_vs_cypher.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_sql_vs_cypher.py) | Multi-tier comparison across 13 AML questions (Easy/Med/Hard) evaluating SQL table joins vs Cypher pattern hops. |
| [`bench_seocho_optimizations.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_seocho_optimizations.py) | Evaluates SEOCHO 3-pillar optimizations: Hub Defense (`USING INDEX`), Reciprocal Ambiguity, and GOpt AST Label Pruning. |
| [`bench_fibo_robustness.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_fibo_robustness.py) | FIBO standards benchmark evaluating Multi-tier UBO Shell Ring and Loan-Guarantee Integration on SF100 (1M+ edges). |
| [`bench_scale_factor.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_scale_factor.py) | Scale factor latency scaling runner (SF1 vs SF10 vs SF100) testing join explosion penalties. |
| [`bench_typefilter.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_typefilter.py) | Hand-applied GOpt `TypeFilterRemovalRule` evaluating redundant schema label removal impact on DB hits and latency. |
| [`bench_bridge2.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_bridge2.py) | Evaluates the 3 boundaries of Bridge 2 (Encoding, Transport, Runtime, Concurrency). |
| [`bench_driver_memory.py`](file:///home/hadry/lab/AIsummit26/scripts/bench_driver_memory.py) | Memory allocation and deserialization profiler for Python vs Rust PackStream decoders. |

---

### 3. 🤖 Agent Orchestration & Live Trace
| Script | Description |
| :--- | :--- |
| [`live_trace_demo.py`](file:///home/hadry/lab/AIsummit26/scripts/live_trace_demo.py) | Real-time live visualizer tracing user question ➔ intent extraction ➔ Text2Cypher ➔ guardrail ➔ GDBMS ➔ augmented answer. |
| [`live_sql_vs_cypher_experiment.py`](file:///home/hadry/lab/AIsummit26/scripts/live_sql_vs_cypher_experiment.py) | Live LLM caller querying MARA Cloud (`gpt-oss-120b`) testing 4 arms in real time. |
| [`agent_interaction.py`](file:///home/hadry/lab/AIsummit26/scripts/agent_interaction.py) | Main two-stage evaluation harness measuring live LLM episodes across the 4 agent arms (`labels`, `ontology`, `guardrail`, `plan`). |
| [`agent_seocho_openai.py`](file:///home/hadry/lab/AIsummit26/scripts/agent_seocho_openai.py) | OpenAI Agents SDK (`openai-agents-python`) native integration with SEOCHO ontology, Guardrails, and OpenTelemetry spans. |
| [`replay_p99.py`](file:///home/hadry/lab/AIsummit26/scripts/replay_p99.py) | Model-free replay engine executing settled queries 100x to extract exact server-side p99 latencies without LLM variance. |

---

### 4. 📊 Analysis & Reporting
| Script | Description |
| :--- | :--- |
| [`report_interaction.py`](file:///home/hadry/lab/AIsummit26/scripts/report_interaction.py) | Generates markdown summary tables from interaction & replay JSON results. |
| [`rescore_execution.py`](file:///home/hadry/lab/AIsummit26/scripts/rescore_execution.py) | Re-scores model outputs against gold truth tables with relaxed formatting tolerances. |
| [`rescore_strict.py`](file:///home/hadry/lab/AIsummit26/scripts/rescore_strict.py) | Exact cryptographic / mathematical match evaluator for gold typologies. |
| [`check_chart_provenance.py`](file:///home/hadry/lab/AIsummit26/scripts/check_chart_provenance.py) | Audits zero-delta integrity between raw JSON episode metrics and published chart figures. |
| [`measure_format_tokens.py`](file:///home/hadry/lab/AIsummit26/scripts/measure_format_tokens.py) | Evaluates tokenizer token consumption across JSON, CSV, and Markdown tabular outputs. |

---

### 5. 📈 Deck & Publication Plotting
| Script | Output Figure | Description |
| :--- | :--- | :--- |
| [`plot_overview.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_overview.py) | `figures/overview-p50.svg` | Main talk overview chart: p50 latency vs accuracy across scales. |
| [`plot_interaction.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_interaction.py) | `figures/overview-p99.svg` | Headline chart: p99 latency by question and agent design arm. |
| [`plot_depth.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_depth.py) | `figures/engineering-detail.svg` | 4-plane physical bottleneck breakdown (Encoding, Transport, Runtime, Concurrency). |
| [`plot_in_context.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_in_context.py) | `figures/in-context.svg` | In-context return regime comparison (JSON vs CSV vs Blind). |
| [`plot_plan_hints.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_plan_hints.py) | `figures/plan-hints-ab.svg` | Planner hints (`USING INDEX` vs `USING JOIN`) A/B evaluation. |
| [`plot_tradeoff.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_tradeoff.py) | `figures/slo-tradeoff.svg` | Accuracy vs latency SLA Pareto frontier. |
| [`plot_estimate_error.py`](file:///home/hadry/lab/AIsummit26/scripts/plot_estimate_error.py) | `figures/estimate-error.svg` | Planner cardinal estimation error on power-law graphs. |

---

### 6. 🔍 Smoke & Sanity Checks
| Script | Description |
| :--- | :--- |
| [`smoke_seocho.py`](file:///home/hadry/lab/AIsummit26/scripts/smoke_seocho.py) | Validates installed SEOCHO package version, ontology parsing, and guardrail policies. |
