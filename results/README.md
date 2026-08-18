# Results Directory Structure

All evaluation metrics, benchmark runs, agent interaction episodes, and interface benchmarks are systematically organized into categorized directories:

```
results/
├── runs/                     # 🚀 Timestamped benchmark runs with full hardware/software provenance & plots
│   ├── <timestamp>_<suite>/
│   │   ├── hardware.json     # CPU, RAM, GPU, OS platform & architecture
│   │   ├── software.json     # Python version, Git commit, package versions (seocho, duckdb, openai)
│   │   ├── metrics.json      # Structured numerical output metrics
│   │   ├── summary.md        # Human-readable markdown evaluation report
│   │   └── plots/            # Generated high-resolution SVG visualizations
│   └── LATEST                # Symlink / pointer to the most recent run
│
├── scenarios/                # 🧪 Core evaluation scenarios & structured comparison benchmarks
│   ├── bench_sql_vs_cypher.json
│   ├── bench_seocho_optimizations.json
│   ├── bench_fibo_robustness.json
│   ├── bench_scale_factor.json
│   ├── live_llm_experiment.json
│   ├── plan_hints_ab.json
│   └── join_hints_ab.json
│
├── episodes/                 # 🤖 Large-scale agent interaction episodes (819 measured episodes & replays)
│   ├── agent_interaction.json
│   ├── replay_p99.json
│   ├── in_context_arms.json
│   ├── in_context_arms.manifest.json
│   ├── in_context_deepseek.json
│   └── in_context_deepseek.manifest.json
│
├── detection/                # 🔍 Planted typology detection workloads across scales (SF1 ~ SF1000)
│   ├── sf1_real.json, sf1_unif.json
│   ├── sf10_real.json, sf10_unif.json
│   ├── sf100_real.json, sf100_unif.json
│   └── sf1000_real.json, sf1000_unif.json
│
├── interface/                # ⚡ Driver, decoder, memory, and transport benchmarks (Rust vs Python)
│   ├── bench_bridge2_20260808.txt
│   ├── bench_driver_cpu_20260808.txt
│   ├── bench_driver_memory_20260808.txt
│   ├── driver_memory_pure-python_*.json
│   ├── driver_memory_rust_*.json
│   ├── neo4rs_native_20260808.json
│   ├── multiagent_mix_*.json
│   ├── multiagent_scale_*.json
│   ├── multiagent_dedup_*.json
│   ├── multiagent_contend_*.json
│   └── format_tokens.json
│
└── analysis/                 # 📊 Rescoring & post-processing validation outputs
    ├── rescore_strict.json
    └── rescore_execution.json
```
