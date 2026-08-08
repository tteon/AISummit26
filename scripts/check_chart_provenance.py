#!/usr/bin/env python3
"""Every number on the deck's two charts, traced to the measurement file it came from.

The overview chart computes straight from results/agent_interaction.json at render time, so
it cannot drift; this script spot-recomputes it anyway. The engineering-detail chart carries
hardcoded constants, and a constant is a claim — each one is checked against the
machine-readable result that produced it. Exit code 1 on any mismatch.

  python scripts/check_chart_provenance.py
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

FAIL: list[str] = []


def check(name: str, chart_value, source_value, source: str, tol: float = 0.05) -> None:
    if isinstance(chart_value, (int, float)):
        ok = abs(chart_value - source_value) <= tol * max(abs(source_value), 1e-9)
    else:
        ok = chart_value == source_value
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:42} chart={chart_value!r:>12} "
          f"source={source_value!r:>12}  <- {source}")
    if not ok:
        FAIL.append(name)


def main() -> None:
    bench = Path("results/bench")

    print("[encoding panel] <- results/bench/format_tokens.json")
    tokens = json.loads((bench / "format_tokens.json").read_text())["tokens"]
    check("JSON tokens, 200 rows", 9017, tokens["json"], "format_tokens.json", 0)
    check("markdown tokens", 6221, tokens["markdown_table"], "format_tokens.json", 0)
    check("CSV tokens", 5211, tokens["csv"], "format_tokens.json", 0)

    print("[transport panel] <- results/bench_bridge2_20260808.txt")
    txt = Path("results/bench_bridge2_20260808.txt").read_text()
    med = {m.group(1).strip(): float(m.group(2))
           for m in re.finditer(r"^\s{2}(\S.*?)\s{2,}median\s+([\d.]+) ms", txt, re.M)}
    check("HTTP full body ms", 397.65, med["http full body"], "bridge2 txt", 0)
    check("Bolt early-stop ms", 12.24, med["bolt stop after 50"], "bridge2 txt", 0)
    check("LIMIT in query ms", 1.44, med["either, LIMIT in query"], "bridge2 txt", 0)

    print("[runtime panel] <- results/bench/driver_memory_*.json")
    pure = json.loads(sorted(bench.glob("driver_memory_pure-python_*.json"))[-1].read_text())
    rust = json.loads(sorted(bench.glob("driver_memory_rust_*.json"))[-1].read_text())
    p100k = next(r for r in pure["row_sweep"] if r["rows"] == 100_000)
    r100k = next(r for r in rust["row_sweep"] if r["rows"] == 100_000)
    check("pure us/row", 20.8, p100k["client_cpu_us_per_row"], "driver_memory pure json")
    check("rust us/row", 15.5, r100k["client_cpu_us_per_row"], "driver_memory rust json")
    check("server us/row", 2.9, p100k["server_cpu_ms_per_fetch"] * 1000 / 100_000,
          "driver_memory pure json")
    check("346 B/row (pure)", 346, p100k["py_alloc_bytes_per_row"],
          "driver_memory pure json", 0.01)
    check("346 B/row (rust)", 346, r100k["py_alloc_bytes_per_row"],
          "driver_memory rust json", 0.01)

    print("[concurrency panel] <- driver_memory jsons + neo4rs_native json")
    for label, chart, src, key in [
        ("threads-pure p50s", [43.8, 91.4, 265.9, 769.3], pure, "concurrency_threads"),
        ("threads-rust p50s", [33.1, 68.3, 214.8, 678.7], rust, "concurrency_threads"),
        ("procs-pure p50s", [45.3, 48.2, 61.4, 81.2], pure, "concurrency_procs"),
    ]:
        actual = [row["p50_ms"] for row in src[key]]
        for c, a, w in zip(chart, actual, (1, 2, 4, 8)):
            check(f"{label} w={w}", c, a, f"{key} json")
    neo = json.loads((bench / "neo4rs_native_20260808.json").read_text())
    for c, a, w in zip([4.6, 5.6, 6.1, 7.7],
                       neo["series_p50_ms"]["neo4rs_single_process"], (1, 2, 4, 8)):
        check(f"neo4rs p50 w={w}", c, a, "neo4rs_native json", 0)

    print("[overview chart] <- results/agent_interaction.json (computed at render time)")
    eps = json.loads(Path("results/agent_interaction.json").read_text())["episodes"]
    check("episode count", 819, len(eps), "agent_interaction.json", 0)
    check("arm count", 7, len({e["arm"] for e in eps}), "agent_interaction.json", 0)
    spot = statistics.median([e["db_hits"] for e in eps
                              if e["arm"] == "in_context_csv" and e["sf"] == 100
                              and e["question_id"] == "int_hard_2"])
    print(f"  [info] spot recompute int_hard_2/csv/SF100 median db_hits = {spot:,.0f} "
          f"(rendered directly, nothing hardcoded)")

    print()
    if FAIL:
        print(f"FAILED: {len(FAIL)} mismatches -> {FAIL}")
        sys.exit(1)
    print("all chart constants trace to recorded measurements")


if __name__ == "__main__":
    main()
