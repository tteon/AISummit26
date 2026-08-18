# 🚀 Benchmark Run Report: quick-smoke-suite

> **Generated At**: `2026-08-18T06:29:19+00:00` | **Suite Version**: `1.0.0`

## 💻 Execution Environment Provenance

### Hardware Specifications
- **CPU**: 11th Gen Intel(R) Core(TM) i7-11700 @ 2.50GHz (16 Cores)
- **Memory**: 58.0 GB Available / 62.55 GB Total
- **Platform**: `Linux-6.8.0-136-generic-x86_64-with-glibc2.35` (x86_64)
- **GPU**: `NVIDIA GeForce RTX 3070, 8192 MiB, 595.84`

### Software & Dependency Versions
- **Python**: `3.10.12` (`/usr`)
- **Git Commit**: `e94c060035690f3bd71e5d86c5b052791e15866f` (Branch: `main`, Dirty: `True`)
- **Key Packages**: `seocho=0.2.0`, `duckdb=1.5.3`, `openai=2.41.0`
- **Target Model**: `gpt-oss-120b` via `mara`

---

## 📊 Executed Benchmark Suites

| Suite Name | Execution Time (s) | Records / Questions | Status |
|:---|:---:|:---:|:---:|
| `sql_vs_cypher` | **0.0s** | 2 items | ✅ PASSED |
| `seocho_optimizations` | **2.15s** | 1 items | ✅ PASSED |
| `fibo_robustness` | **0.01s** | 1 items | ✅ PASSED |

---

## 📈 Generated Visualizations

- [Structural Complexity Plot (SQL vs Cypher)](plots/sql_vs_cypher_complexity.svg)
- [Suite Latency Breakdown Plot](plots/suite_latency.svg)

---

## 📝 Detailed Suite Metrics

### Suite: `sql_vs_cypher`
```json
[
  {
    "question_id": "ext_easy_1",
    "tier": "easy",
    "sql_joins": 0,
    "cypher_hops": 0,
    "sql_len": 118,
    "cypher_len": 137,
    "ratio": 0.86
  },
  {
    "question_id": "ext_med_1",
    "tier": "medium",
    "sql_joins": 1,
    "cypher_hops": 1,
    "sql_len": 196,
    "cypher_len": 173,
    "ratio": 1.13
  }
]
```

### Suite: `seocho_optimizations`
```json
[
  {
    "question_id": "ext_hard_1",
    "latency_ms": 1860.4,
    "pruned_labels_count": 1,
    "has_using_index": true
  }
]
```

### Suite: `fibo_robustness`
```json
[
  {
    "scenario": "SF10_UBO_Transfer_Join",
    "scale": 10,
    "execution_ms": 3.69,
    "records_matched": 100458
  }
]
```
