#!/usr/bin/env python3
"""Unified Benchmark CLI Entrypoint (AIPerf-Style Declarative Runner).

Usage:
  # Run full benchmark suite from YAML config:
  python runner.py run --config configs/aml_suite.yaml

  # Run quick smoke test suite:
  python runner.py run --config configs/quick_smoke.yaml

  # Run a single specific suite:
  python runner.py run --suite sql_vs_cypher

  # List all available benchmark suites in config:
  python runner.py list --config configs/aml_suite.yaml

  # Interactive Live E2E Trace Demo:
  python runner.py demo -q "계좌 1001번의 송금 수취인 상위 5명을 찾아줘"
"""
from __future__ import annotations

import sys
import argparse
import asyncio
from pathlib import Path

# Add scripts and repo root to sys.path for backward-compatible module resolution
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
for sub in ["scripts", "scripts/benchmarks", "scripts/agents", "scripts/analysis", "scripts/data", "scripts/plotting", "scripts/smoke"]:
    p = str(REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from harness.config import BenchmarkConfig
from harness.runner import BenchmarkRunner


def main():
    parser = argparse.ArgumentParser(
        description="AIPerf-Style Declarative Benchmark Runner for Financial AML Graph AI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # 1. 'run' command
    run_parser = subparsers.add_parser("run", help="Run declarative benchmark suites from YAML config")
    run_parser.add_argument("-c", "--config", type=str, default="configs/aml_suite.yaml", help="Path to YAML config file")
    run_parser.add_argument("-s", "--suite", type=str, help="Target specific suite name to run")

    # 2. 'list' command
    list_parser = subparsers.add_parser("list", help="List suites defined in the config")
    list_parser.add_argument("-c", "--config", type=str, default="configs/aml_suite.yaml", help="Path to YAML config file")

    # 3. 'demo' command
    demo_parser = subparsers.add_parser("demo", help="Run interactive live E2E pipeline visualizer")
    demo_parser.add_argument("-q", "--question", nargs="+", type=str, help="One or more natural language questions to test")
    demo_parser.add_argument("-f", "--file", type=str, help="Path to file (.txt, .yaml, .json) containing a list of questions")
    demo_parser.add_argument("-p", "--preset", type=str, help="Preset question ID (e.g. ext_med_1)")
    demo_parser.add_argument("-a", "--anchor", type=int, default=1001, help="Anchor account number (default: 1001)")
    demo_parser.add_argument("-s", "--sf", type=int, default=10, help="Scale factor (default: 10)")

    args = parser.parse_args()

    if args.command == "run" or args.command is None:
        config_path = getattr(args, "config", "configs/aml_suite.yaml") or "configs/aml_suite.yaml"
        if not Path(config_path).exists():
            print(f"❌ Error: Config file '{config_path}' not found.")
            sys.exit(1)
        
        cfg = BenchmarkConfig.from_yaml(config_path)
        runner = BenchmarkRunner(cfg)
        asyncio.run(runner.run_all(target_suite=getattr(args, "suite", None)))

    elif args.command == "list":
        cfg = BenchmarkConfig.from_yaml(args.config)
        print(f"\n📋 Suites in Benchmark Configuration: '{cfg.name}' (v{cfg.version})")
        print(f"Description: {cfg.description.strip()}")
        print("-" * 80)
        for s in cfg.suites:
            status = "✅ Enabled" if s.enabled else "⏸️ Disabled"
            print(f"  • {s.name:<25} [{status}] : {s.description}")
        print("-" * 80 + "\n")

    elif args.command == "demo":
        from scripts.agents.live_trace_demo import trace_e2e, PRESET_QUESTIONS, load_questions_from_file
        question_items = []
        if args.file:
            question_items = load_questions_from_file(args.file, default_anchor=args.anchor)
        elif args.question:
            for q in args.question:
                question_items.append({"question": q, "anchor": args.anchor})
        elif args.preset:
            q_text = PRESET_QUESTIONS[args.preset]["question"].format(a=args.anchor)
            question_items.append({"question": q_text, "anchor": args.anchor})
        else:
            question_items.append({
                "question": "내가 송금한 계좌들의 실제 소유자(Person 또는 Company) 상위 5명은 누구인가요?",
                "anchor": args.anchor
            })

        for idx, item in enumerate(question_items, 1):
            if len(question_items) > 1:
                print(f"\n\033[95m\033[1m=== Running Question {idx}/{len(question_items)} ===\033[0m")
            asyncio.run(trace_e2e(item["question"], anchor_acct=item["anchor"], sf=args.sf))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
