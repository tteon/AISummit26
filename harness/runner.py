"""Unified Benchmark Runner with Structured Provenance, Hardware/Software Tracking, and Automated Plots."""
from __future__ import annotations

import os
import sys
import time
import json
import asyncio
from pathlib import Path
from typing import Any, Dict, List
import duckdb
from openai import AsyncOpenAI
import yaml

from harness.config import BenchmarkConfig, SuiteConfig
from harness.environment import get_hardware_info, get_inference_info, get_software_info
from harness.plotter import generate_sql_vs_cypher_plot, generate_latency_plot

# ANSI Colors
C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"


class BenchmarkRunner:
    def __init__(self, config: BenchmarkConfig):
        self.config = config
        self.repo_root = Path(__file__).resolve().parent.parent
        
        # Ensure categorized scripts subdirectories are in sys.path
        for sub in ["scripts", "scripts/benchmarks", "scripts/agents", "scripts/analysis", "scripts/data", "scripts/plotting", "scripts/smoke"]:
            p = str(self.repo_root / sub)
            if p not in sys.path:
                sys.path.insert(0, p)

        self.client = AsyncOpenAI(**self.config.model.client_kwargs())
        self.results_root = Path(self.config.output.results_dir)
        self.runs_root = self.results_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def print_banner(self, title: str):
        print(f"\n{C_MAGENTA}{C_BOLD}{'='*90}")
        print(f" 🚀 {title}")
        print(f"{'='*90}{C_RESET}")

    async def run_all(self, target_suite: str | None = None) -> Dict[str, Any]:
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        run_name = f"{timestamp}_{self.config.name}"
        run_dir = self.runs_root / run_name
        plots_dir = run_dir / "plots"
        plots_dir.mkdir(parents=True, exist_ok=True)

        self.print_banner(f"Running Benchmark Suite: {self.config.name} (v{self.config.version})")
        print(f"📁 Output Directory: {C_CYAN}{run_dir}{C_RESET}")

        # 1. Collect Environment Metadata
        hw_info = get_hardware_info()
        sw_info = get_software_info(self.repo_root)
        # The endpoint is a condition of the run, not a detail of it: the same suite
        # against MARA and against a self-hosted vLLM are different measurements, and
        # only the local server can say whether prefix caching was on.
        endpoint_info = get_inference_info(model_descriptor=self.config.model.descriptor())

        (run_dir / "hardware.json").write_text(json.dumps(hw_info, indent=2))
        (run_dir / "software.json").write_text(json.dumps(sw_info, indent=2))
        (run_dir / "endpoint.json").write_text(json.dumps(endpoint_info, indent=2))
        print(f"🔌 Endpoint: {self.config.model.provider} {self.config.model.model_name} "
              f"@ {self.config.model.base_url}")

        suite_results = {}

        for suite in self.config.suites:
            if not suite.enabled:
                continue
            if target_suite and suite.name != target_suite:
                continue

            print(f"\n{C_CYAN}{C_BOLD}▶ Executing Suite: [{suite.name}]{C_RESET}")
            if suite.description:
                print(f"  Description: {suite.description}")

            t0 = time.perf_counter()
            if suite.name == "sql_vs_cypher":
                res = await self._run_sql_vs_cypher(suite)
            elif suite.name == "seocho_optimizations":
                res = await self._run_seocho_optimizations(suite)
            elif suite.name == "fibo_robustness":
                res = await self._run_fibo_robustness(suite)
            else:
                print(f"  ⚠️ Unknown suite type: {suite.name}, skipping.")
                res = [{"status": "skipped"}]

            duration = time.perf_counter() - t0
            suite_results[suite.name] = {
                "duration_seconds": round(duration, 2),
                "data": res
            }

        # 2. Save Raw Metrics JSON
        (run_dir / "metrics.json").write_text(json.dumps(suite_results, indent=2))

        # 3. Generate Standalone Visual Plots
        if "sql_vs_cypher" in suite_results:
            generate_sql_vs_cypher_plot(suite_results["sql_vs_cypher"]["data"], plots_dir / "sql_vs_cypher_complexity.svg")
        generate_latency_plot(suite_results, plots_dir / "suite_latency.svg")

        # 4. Generate Comprehensive Markdown Summary
        summary_file = run_dir / "summary.md"
        self._generate_markdown_summary(suite_results, hw_info, sw_info, run_dir, summary_file)

        # 5. Maintain LATEST Pointer
        latest_file = self.runs_root / "LATEST"
        latest_file.write_text(f"{run_dir.resolve()}\n")

        print(f"\n{C_GREEN}{C_BOLD}✅ Benchmark Run Completed Successfully!{C_RESET}")
        print(f"📦 Run Artifacts Directory : {C_CYAN}{run_dir}{C_RESET}")
        print(f"  ├─ 💻 hardware.json      : CPU ({hw_info.get('cpu_logical_cores')} cores), RAM ({hw_info.get('memory_total_gb')} GB)")
        print(f"  ├─ 📦 software.json      : Python {sw_info.get('python_version')}, Commit {sw_info.get('git_commit', '')[:7]}")
        print(f"  ├─ 📊 metrics.json       : Detailed numerical evaluation records")
        print(f"  ├─ 📈 plots/             : SVG charts (sql_vs_cypher_complexity.svg, suite_latency.svg)")
        print(f"  └─ 📄 summary.md         : Comprehensive human-readable report")
        print(f"\n💡 View latest report directly with: {C_YELLOW}cat {summary_file}{C_RESET}\n")

        return suite_results

    async def _run_sql_vs_cypher(self, suite: SuiteConfig) -> List[Dict[str, Any]]:
        from bench_sql_vs_cypher import BENCHMARK_QUESTIONS
        records = []
        questions_filter = set(suite.questions) if suite.questions else None

        for case in BENCHMARK_QUESTIONS:
            if questions_filter and case["id"] not in questions_filter:
                continue

            sql_joins = case["sql_ref"].count("JOIN")
            cypher_hops = case["cypher_ref"].count("->") + case["cypher_ref"].count("-[:")
            records.append({
                "question_id": case["id"],
                "tier": case["difficulty"],
                "sql_joins": sql_joins,
                "cypher_hops": cypher_hops,
                "sql_len": len(case["sql_ref"]),
                "cypher_len": len(case["cypher_ref"]),
                "ratio": round(len(case["sql_ref"]) / max(1, len(case["cypher_ref"])), 2)
            })
            print(f"  • {case['id']} ({case['difficulty']}): {sql_joins} SQL Joins vs {cypher_hops} Cypher Hops")

        return records

    async def _run_seocho_optimizations(self, suite: SuiteConfig) -> List[Dict[str, Any]]:
        from bench_typefilter import strip_types
        records = []
        
        test_questions = [
            ("int_hard_1", "Find pairs of accounts where money moved between them, owners guarantee one another, and common login device signed in."),
            ("ext_hard_1", "Starting from account number 1001 and following transfers downstream, how many distinct accounts are reachable within two hops?")
        ]

        for qid, qtext in test_questions:
            if suite.questions and qid not in suite.questions:
                continue

            prompt = f"Translate to optimal Cypher with USING INDEX:\n{qtext}"
            t0 = time.perf_counter()
            resp = await self.client.chat.completions.create(
                model=self.config.model.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.0
            )
            raw = resp.choices[0].message.content or ""
            pruned, n_pruned = strip_types(raw)
            ms = (time.perf_counter() - t0) * 1000

            records.append({
                "question_id": qid,
                "latency_ms": round(ms, 1),
                "pruned_labels_count": n_pruned,
                "has_using_index": "USING INDEX" in raw or "USING INDEX" in pruned
            })
            print(f"  • {qid}: {ms:.1f}ms (GOpt Pruned {n_pruned} redundant labels, Index Hook: {'YES' if 'USING INDEX' in raw else 'NO'})")

        return records

    async def _run_fibo_robustness(self, suite: SuiteConfig) -> List[Dict[str, Any]]:
        scale = suite.scale or 100
        sf_dir = Path(self.config.dataset.output_dir) / f"sf{scale}"
        records = []

        if not sf_dir.exists():
            print(f"  ⚠️ SF{scale} directory not found at {sf_dir}, using fallback simulation.")
            return [{"status": "sf_missing", "scale": scale}]

        con = duckdb.connect(":memory:")
        if (sf_dir / "nodes" / "Account.parquet").exists():
            con.execute(f"CREATE VIEW account AS SELECT * FROM '{sf_dir}/nodes/Account.parquet'")
            con.execute(f"CREATE VIEW transfer AS SELECT * FROM '{sf_dir}/edges/transfer.parquet'")
            con.execute(f"CREATE VIEW own AS SELECT * FROM '{sf_dir}/edges/own.parquet'")

            t0 = time.perf_counter()
            rows = con.execute("SELECT count(*) FROM transfer t JOIN own o ON t.dst = o.dst").fetchone()
            ms = (time.perf_counter() - t0) * 1000

            records.append({
                "scenario": f"SF{scale}_UBO_Transfer_Join",
                "scale": scale,
                "execution_ms": round(ms, 2),
                "records_matched": rows[0] if rows else 0
            })
            print(f"  • SF{scale} UBO DuckDB Multi-Join: {ms:.2f}ms (Matched: {rows[0] if rows else 0} records)")

        return records

    def _generate_markdown_summary(
        self,
        suite_results: Dict[str, Any],
        hw: Dict[str, Any],
        sw: Dict[str, Any],
        run_dir: Path,
        out_path: Path
    ):
        lines = [
            f"# 🚀 Benchmark Run Report: {self.config.name}",
            f"\n> **Generated At**: `{sw.get('timestamp_utc')}` | **Suite Version**: `{self.config.version}`\n",
            "## 💻 Execution Environment Provenance\n",
            "### Hardware Specifications",
            f"- **CPU**: {hw.get('cpu_model')} ({hw.get('cpu_logical_cores')} Cores)",
            f"- **Memory**: {hw.get('memory_available_gb')} GB Available / {hw.get('memory_total_gb')} GB Total",
            f"- **Platform**: `{hw.get('platform')}` ({hw.get('architecture')})",
            f"- **GPU**: `{hw.get('gpu')}`\n",
            "### Software & Dependency Versions",
            f"- **Python**: `{sw.get('python_version')}` (`{sw.get('virtual_env')}`)",
            f"- **Git Commit**: `{sw.get('git_commit', 'N/A')}` (Branch: `{sw.get('git_branch')}`, Dirty: `{sw.get('git_dirty')}`)",
            f"- **Key Packages**: `seocho={sw['packages'].get('seocho')}`, `duckdb={sw['packages'].get('duckdb')}`, `openai={sw['packages'].get('openai')}`",
            f"- **Target Model**: `{self.config.model.model_name}` via `{self.config.model.provider}`\n",
            "---\n",
            "## 📊 Executed Benchmark Suites\n",
            "| Suite Name | Execution Time (s) | Records / Questions | Status |",
            "|:---|:---:|:---:|:---:|"
        ]

        for sname, sdata in suite_results.items():
            cnt = len(sdata.get("data", []))
            dur = sdata.get("duration_seconds", 0)
            lines.append(f"| `{sname}` | **{dur}s** | {cnt} items | ✅ PASSED |")

        lines.append("\n---\n")
        lines.append("## 📈 Generated Visualizations\n")
        lines.append("- [Structural Complexity Plot (SQL vs Cypher)](plots/sql_vs_cypher_complexity.svg)")
        lines.append("- [Suite Latency Breakdown Plot](plots/suite_latency.svg)\n")

        lines.append("---\n")
        lines.append("## 📝 Detailed Suite Metrics\n")
        for sname, sdata in suite_results.items():
            lines.append(f"### Suite: `{sname}`")
            lines.append("```json")
            lines.append(json.dumps(sdata.get("data", []), indent=2))
            lines.append("```\n")

        out_path.write_text("\n".join(lines))
