# Benchmark Evaluation Report: quick-smoke-suite
**Version**: 1.0.0 | **Model**: `gpt-oss-120b` | **Provider**: `mara`

## Summary of Executed Suites

| Suite Name | Execution Time (s) | Status |
|:---|:---:|:---:|
| `sql_vs_cypher` | 0.0s | ✅ PASSED |
| `seocho_optimizations` | 2.22s | ✅ PASSED |
| `fibo_robustness` | 0.01s | ✅ PASSED |

---

## Detailed Results

### Suite: `sql_vs_cypher`
```json
[
  {
    "id": "ext_easy_1",
    "tier": "easy",
    "sql_joins": 0,
    "cypher_hops": 0,
    "sql_len": 118,
    "cypher_len": 137,
    "ratio": 0.86
  },
  {
    "id": "ext_med_1",
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
    "latency_ms": 1939.8,
    "pruned_labels_count": 1,
    "has_using_index": true
  }
]
```

### Suite: `fibo_robustness`
```json
[
  {
    "scenario": "SF100_UBO_Transfer_Join",
    "scale": 10,
    "execution_ms": 3.58,
    "records_matched": 100458
  }
]
```
